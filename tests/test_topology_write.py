"""
Tests app/topology_write.py's endpoints against a fake TopologyBackend -
a real TopologyBackend opens a real pyvmomi/GNS3 session (see its own
module docstring), same reason app_state's esxi_connection is a
MagicMock rather than a real ESXiConnection. Each fake method mimics the
one thing these tests actually need to prove: the topology YAML file
ends up changed on disk and app.state.graph gets reloaded afterward -
whether the change is deploy-live-accurate is TopologyBuilder's own,
already-tested concern.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml


class _FakeBackend:
    def __init__(self):
        self.calls: list[tuple] = []

    def _read(self, topology_file):
        return yaml.safe_load(Path(topology_file).read_text())

    def _write(self, topology_file, data):
        Path(topology_file).write_text(yaml.safe_dump(data))

    def add_node(self, topology_file, name, role, image):
        self.calls.append(("add_node", topology_file, name, role, image))
        data = self._read(topology_file)
        data["nodes"].append({"names": [name], "role": role, "image": image})
        self._write(topology_file, data)

    def remove_node(self, topology_file, name):
        self.calls.append(("remove_node", topology_file, name))
        data = self._read(topology_file)
        data["nodes"] = [n for n in data["nodes"] if name not in n["names"]]
        data["edges"] = [e for e in data["edges"] if name not in (e[0], e[2])]
        self._write(topology_file, data)

    def add_link(self, topology_file, node1, if1, node2, if2):
        self.calls.append(("add_link", topology_file, node1, if1, node2, if2))
        data = self._read(topology_file)
        data.setdefault("edges", []).append([node1, if1, node2, if2])
        self._write(topology_file, data)

    def remove_link(self, topology_file, node1, node2):
        self.calls.append(("remove_link", topology_file, node1, node2))
        data = self._read(topology_file)
        data["edges"] = [e for e in data["edges"] if {e[0], e[2]} != {node1, node2}]
        self._write(topology_file, data)


def test_add_node_calls_backend_and_reloads_graph(client, app_state, temp_topology_file):
    fake = _FakeBackend()
    app_state.topology_backend = fake

    resp = client.post("/nodes", json={"name": "PC1", "role": "PC", "image": "VPCS"})

    assert resp.status_code == 200, resp.text
    assert fake.calls == [("add_node", str(temp_topology_file), "PC1", "PC", "VPCS")]
    assert "PC1" in app_state.graph.nodes


def test_add_node_value_error_maps_to_400(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.add_node.side_effect = ValueError("node 'R1' already exists")
    app_state.topology_backend = fake

    resp = client.post(
        "/nodes", json={"name": "R1", "role": "ROUTER", "image": "Cisco IOSv 15.6(1)T"}
    )

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_add_node_runtime_error_maps_to_502(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.add_node.side_effect = RuntimeError("deploy failed")
    app_state.topology_backend = fake

    resp = client.post("/nodes", json={"name": "PC1", "role": "PC", "image": "VPCS"})

    assert resp.status_code == 502


def test_remove_node_calls_backend_and_reloads_graph(client, app_state, temp_topology_file):
    fake = _FakeBackend()
    app_state.topology_backend = fake

    resp = client.delete("/nodes/SW1")

    assert resp.status_code == 200, resp.text
    assert fake.calls == [("remove_node", str(temp_topology_file), "SW1")]
    assert "SW1" not in app_state.graph.nodes
    assert "gi0/0" not in app_state.graph.nodes["R1"].interfaces


def test_remove_node_value_error_maps_to_404(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.remove_node.side_effect = ValueError("no node named 'GHOST'")
    app_state.topology_backend = fake

    resp = client.delete("/nodes/GHOST")

    assert resp.status_code == 404


def test_remove_node_runtime_error_maps_to_502(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.remove_node.side_effect = RuntimeError("delete failed")
    app_state.topology_backend = fake

    resp = client.delete("/nodes/SW1")

    assert resp.status_code == 502


def test_add_link_calls_backend_and_reloads_graph(client, app_state, temp_topology_file):
    fake = _FakeBackend()
    app_state.topology_backend = fake

    resp = client.post(
        "/links", json={"node1": "VM1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet1"}
    )

    assert resp.status_code == 200, resp.text
    assert fake.calls == [
        ("add_link", str(temp_topology_file), "VM1", "eth0", "SW1", "Ethernet1")
    ]
    assert app_state.graph.nodes["VM1"].interfaces["eth0"].neighbour.name == "SW1"


def test_add_link_value_error_maps_to_400(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.add_link.side_effect = ValueError("interface already in use")
    app_state.topology_backend = fake

    resp = client.post(
        "/links", json={"node1": "R1", "if1": "gi0/0", "node2": "VM1", "if2": "eth0"}
    )

    assert resp.status_code == 400


def test_add_link_runtime_error_maps_to_502(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.add_link.side_effect = RuntimeError("deploy failed")
    app_state.topology_backend = fake

    resp = client.post(
        "/links", json={"node1": "VM1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet1"}
    )

    assert resp.status_code == 502


def test_remove_link_calls_backend_and_reloads_graph(client, app_state, temp_topology_file):
    fake = _FakeBackend()
    app_state.topology_backend = fake

    resp = client.delete("/links/R1/SW1")

    assert resp.status_code == 200, resp.text
    assert fake.calls == [("remove_link", str(temp_topology_file), "R1", "SW1")]
    assert app_state.graph.nodes["R1"].interfaces == {}


def test_remove_link_runtime_error_maps_to_502(client, app_state, temp_topology_file):
    fake = MagicMock()
    fake.remove_link.side_effect = RuntimeError("delete failed")
    app_state.topology_backend = fake

    resp = client.delete("/links/R1/SW1")

    assert resp.status_code == 502


def test_templates_returns_gns3_and_esxi_names(client, app_state):
    with patch(
        "app.topology_write.APIHandler.get_gns3_template_names",
        return_value={"VPCS", "Cisco IOSv 15.6(1)T"},
    ), patch(
        "app.topology_write.APIHandler.get_esxi_template_names",
        return_value={"debian", "pfSense"},
    ):
        resp = client.get("/templates")

    assert resp.status_code == 200
    assert resp.json() == {
        "gns3": ["Cisco IOSv 15.6(1)T", "VPCS"],
        "esxi": ["debian", "pfSense"],
    }


def test_templates_timeout_maps_to_503(client, app_state):
    with patch(
        "app.topology_write.APIHandler.get_gns3_template_names",
        side_effect=TimeoutError("template server slow"),
    ):
        resp = client.get("/templates")

    assert resp.status_code == 503
