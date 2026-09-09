from unittest.mock import MagicMock, patch


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
