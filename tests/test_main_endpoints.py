import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import yaml

from src.settings import Settings


def test_health_reports_degraded_state(client, app_state):
    app_state.startup_warning = "GNS3 VM not found"
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["warning"] == "GNS3 VM not found"
    assert body["gns3_project_id"] is None


def test_topology_returns_static_structure_regardless_of_live_state(client, app_state):
    resp = client.get("/topology")
    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["R1"]["environment"] == "ON_GNS3"
    assert by_name["VM1"]["environment"] == "ON_ESXI"
    assert by_name["R1"]["interfaces"]["gi0/0"] == "SW1"


def test_nodes_merges_live_gns3_data_for_gns3_nodes(client, app_state):
    app_state.project_id = "proj-1"
    live = [
        {
            "name": "R1",
            "node_id": "n1",
            "status": "started",
            "console": 5000,
            "console_type": "telnet",
        }
    ]
    with patch("app.main.GNS3Connection.list_project_nodes", return_value=live):
        resp = client.get("/nodes")
    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["R1"]["status"] == "started"
    assert by_name["R1"]["console_type"] == "telnet"


def test_node_ports_returns_real_interfaces_for_gns3_node(client, app_state):
    app_state.project_id = "proj-1"
    live = [
        {
            "name": "R1",
            "node_id": "n1",
            "ports": [{"name": "gi0/0", "adapter_number": 0, "port_number": 0}],
        }
    ]
    with patch("app.main.GNS3Connection.list_project_nodes", return_value=live), patch(
        "app.main.GNS3Connection.list_project_links", return_value=[]
    ):
        resp = client.get("/nodes/R1/ports")
    assert resp.status_code == 200
    assert resp.json() == [{"name": "gi0/0", "in_use": False}]


def test_node_ports_404_for_esxi_node(client, app_state):
    app_state.project_id = "proj-1"
    with patch("app.main.GNS3Connection.list_project_nodes", return_value=[]):
        resp = client.get("/nodes/VM1/ports")
    assert resp.status_code == 404


def test_nodes_resolves_esxi_power_state_independent_of_gns3_project(client, app_state):
    """
    Real regression test for a real bug this session: /nodes used to
    503 its *entire* response whenever no GNS3 project was resolved,
    even though ESXi VM status is a fully independent data source.
    """
    app_state.project_id = None  # no live GNS3 project at all
    fake_vm = MagicMock()
    fake_vm.runtime.powerState = "poweredOn"
    app_state.esxi_connection.get_vm.return_value = fake_vm

    resp = client.get("/nodes")
    assert resp.status_code == 200, resp.text
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["VM1"]["status"] == "poweredOn"


def test_nodes_esxi_status_none_when_vm_not_found(client, app_state):
    app_state.project_id = None
    app_state.esxi_connection.get_vm.return_value = None

    resp = client.get("/nodes")
    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["VM1"]["status"] is None


def test_layout_get_returns_a_position_for_every_node(client, app_state, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.get("/layout")
    assert resp.status_code == 200
    assert set(resp.json()) == {"R1", "SW1", "VM1"}


def test_layout_post_then_get_round_trips(client, app_state, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.post("/layout", json={"name": "R1", "x": 10, "y": 20})
    assert resp.status_code == 200

    resp = client.get("/layout")
    assert resp.json()["R1"] == {"x": 10, "y": 20}


def test_layout_post_rejects_unknown_node(client, app_state, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.post("/layout", json={"name": "GHOST", "x": 0, "y": 0})
    assert resp.status_code == 404


def test_drawings_get_reflects_current_state(client, app_state):
    app_state.last_drawings = []
    resp = client.get("/drawings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_drawings_post_then_get_round_trips(client, app_state):
    drawing = {"id": 1, "type": "rect", "x1": 0, "y1": 0, "x2": 100, "y2": 50, "stroke": "#7cb342"}
    resp = client.post("/drawings", json={"drawings": [drawing]})
    assert resp.status_code == 200

    resp = client.get("/drawings")
    assert resp.json() == [drawing]
    assert app_state.last_drawings == [drawing]


def _valid_bundle(topology_yaml_text: str) -> bytes:
    manifest = {"format_version": 1, "project_name": "test", "exported_at": "now"}
    settings = {
        "esxi": {"ip": Settings.ESXI.IP, "gns3_vm_name": Settings.ESXI.GNS3_VM_NAME},
        "gns3": {"username": Settings.GNS3.USERNAME, "port": Settings.GNS3.PORT},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("topology.yaml", topology_yaml_text)
        zf.writestr("settings.yml", yaml.safe_dump(settings))
        zf.writestr("configs.json", json.dumps({"devices": {}}))
    return buf.getvalue()


def test_export_project_returns_zip_with_attachment_header(client, app_state, temp_topology_file):
    resp = client.get("/project/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert ".topoproj" in resp.headers["content-disposition"]
    assert set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist()) == {
        "manifest.json", "topology.yaml", "settings.yml", "configs.json", "drawings.json",
    }


def test_import_project_happy_path_returns_manifest_and_configs(
    client, app_state, temp_topology_file
):
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(temp_topology_file.read_text())

    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", bundle, "application/zip")},
    )

    assert resp.status_code == 200, resp.text
    fake.deploy_topology.assert_called_once()
    assert resp.json()["manifest"]["format_version"] == 1


def test_import_project_bad_bundle_maps_to_400(client, app_state, temp_topology_file):
    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", b"not a zip", "application/zip")},
    )
    assert resp.status_code == 400


def test_import_project_deploy_failure_maps_to_502(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.deploy_topology.side_effect = RuntimeError("deploy failed")
    app_state.topology_backend = fake
    bundle = _valid_bundle(temp_topology_file.read_text())

    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", bundle, "application/zip")},
    )
    assert resp.status_code == 502
