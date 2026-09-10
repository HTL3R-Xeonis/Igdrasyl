"""
Tests app/lifecycle.py's endpoints. GNS3 actions are verified by patching
GNS3Connection.start_node/stop_node/reload_node (real network calls
tested already in TopologyBuilder itself); ESXi actions by asserting
calls on app_state.esxi_connection, already a MagicMock per conftest.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _live_gns3_node(name: str, node_id: str) -> dict:
    return {
        "name": name,
        "node_id": node_id,
        "status": "started",
        "console": 5000,
        "console_type": "telnet",
    }


def test_start_node_gns3_calls_gns3_connection(client, app_state):
    app_state.project_id = "proj-1"
    with patch(
        "app.lifecycle.GNS3Connection.list_project_nodes",
        return_value=[_live_gns3_node("R1", "node-1")],
    ), patch("app.lifecycle.GNS3Connection.start_node") as mock_start:
        resp = client.post("/nodes/R1/start")

    assert resp.status_code == 200, resp.text
    mock_start.assert_called_once_with(app_state.gns3_vm_ip, 80, "proj-1", "node-1")


def test_stop_node_gns3_calls_gns3_connection(client, app_state):
    app_state.project_id = "proj-1"
    with patch(
        "app.lifecycle.GNS3Connection.list_project_nodes",
        return_value=[_live_gns3_node("R1", "node-1")],
    ), patch("app.lifecycle.GNS3Connection.stop_node") as mock_stop:
        resp = client.post("/nodes/R1/stop")

    assert resp.status_code == 200, resp.text
    mock_stop.assert_called_once_with(app_state.gns3_vm_ip, 80, "proj-1", "node-1")


def test_reload_node_gns3_calls_gns3_connection(client, app_state):
    app_state.project_id = "proj-1"
    with patch(
        "app.lifecycle.GNS3Connection.list_project_nodes",
        return_value=[_live_gns3_node("R1", "node-1")],
    ), patch("app.lifecycle.GNS3Connection.reload_node") as mock_reload:
        resp = client.post("/nodes/R1/reload")

    assert resp.status_code == 200, resp.text
    mock_reload.assert_called_once_with(app_state.gns3_vm_ip, 80, "proj-1", "node-1")


def test_start_node_esxi_calls_power_on_vm(client, app_state):
    fake_vm = MagicMock()
    app_state.esxi_connection.get_vm.return_value = fake_vm

    resp = client.post("/nodes/VM1/start")

    assert resp.status_code == 200, resp.text
    app_state.esxi_connection.power_on_vm.assert_called_once_with(fake_vm)


def test_stop_node_esxi_calls_power_off_vm(client, app_state):
    fake_vm = MagicMock()
    app_state.esxi_connection.get_vm.return_value = fake_vm

    resp = client.post("/nodes/VM1/stop")

    assert resp.status_code == 200, resp.text
    app_state.esxi_connection.power_off_vm.assert_called_once_with(fake_vm)


def test_reload_node_esxi_calls_reset_vm(client, app_state):
    fake_vm = MagicMock()
    app_state.esxi_connection.get_vm.return_value = fake_vm

    resp = client.post("/nodes/VM1/reload")

    assert resp.status_code == 200, resp.text
    app_state.esxi_connection.reset_vm.assert_called_once_with(fake_vm)


def test_start_node_unknown_node_maps_to_404(client, app_state):
    resp = client.post("/nodes/GHOST/start")
    assert resp.status_code == 404


def test_start_node_esxi_vm_not_found_maps_to_404(client, app_state):
    app_state.esxi_connection.get_vm.return_value = None
    resp = client.post("/nodes/VM1/start")
    assert resp.status_code == 404


def test_start_node_no_live_gns3_instance_maps_to_404(client, app_state):
    app_state.project_id = "proj-1"
    with patch("app.lifecycle.GNS3Connection.list_project_nodes", return_value=[]):
        resp = client.post("/nodes/R1/start")
    assert resp.status_code == 404


def test_start_node_gns3_runtime_error_maps_to_502(client, app_state):
    app_state.project_id = "proj-1"
    with patch(
        "app.lifecycle.GNS3Connection.list_project_nodes",
        return_value=[_live_gns3_node("R1", "node-1")],
    ), patch(
        "app.lifecycle.GNS3Connection.start_node",
        side_effect=RuntimeError("start failed"),
    ):
        resp = client.post("/nodes/R1/start")
    assert resp.status_code == 502


def test_stop_node_esxi_runtime_error_maps_to_502(client, app_state):
    fake_vm = MagicMock()
    app_state.esxi_connection.get_vm.return_value = fake_vm
    app_state.esxi_connection.power_off_vm.side_effect = RuntimeError("stop failed")

    resp = client.post("/nodes/VM1/stop")

    assert resp.status_code == 502
