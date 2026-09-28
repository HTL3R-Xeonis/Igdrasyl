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


def test_add_node_calls_backend_and_reloads_graph(
    client, app_state, temp_topology_file
):
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


def test_remove_node_calls_backend_and_reloads_graph(
    client, app_state, temp_topology_file
):
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


def test_add_link_calls_backend_and_reloads_graph(
    client, app_state, temp_topology_file
):
    fake = _FakeBackend()
    app_state.topology_backend = fake

    resp = client.post(
        "/links",
        json={"node1": "VM1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet1"},
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
        "/links",
        json={"node1": "VM1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet1"},
    )

    assert resp.status_code == 502


def test_remove_link_calls_backend_and_reloads_graph(
    client, app_state, temp_topology_file
):
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
    with (
        patch(
            "app.topology_write.APIHandler.get_gns3_template_names",
            return_value={"VPCS", "Cisco IOSv 15.6(1)T"},
        ),
        patch(
            "app.topology_write.APIHandler.get_esxi_template_names",
            return_value={"debian", "pfSense"},
        ),
        patch("app.topology_write._fetch_gns3_templates", return_value=[]),
        patch(
            "app.topology_write._gns3_template_categories",
            return_value={"Cisco IOSv 15.6(1)T": "router", "VPCS": "guest"},
        ),
        patch("app.topology_write._gns3_template_port_specs", return_value={}),
    ):
        resp = client.get("/templates")

    assert resp.status_code == 200
    assert resp.json() == {
        "gns3": ["Cisco IOSv 15.6(1)T", "VPCS"],
        "esxi": ["debian", "pfSense"],
        "gns3_categories": {"Cisco IOSv 15.6(1)T": "router", "VPCS": "guest"},
        "gns3_port_specs": {},
    }


def test_templates_timeout_maps_to_503(client, app_state):
    with patch(
        "app.topology_write.APIHandler.get_gns3_template_names",
        side_effect=TimeoutError("template server slow"),
    ):
        resp = client.get("/templates")

    assert resp.status_code == 503


def test_fetch_gns3_templates_returns_raw_template_list(app_state):
    from app import topology_write

    fake_response = {
        "templates": [
            {"name": "Cisco IOSv 15.9(3)M6", "category": "router"},
            {"name": "Cisco IOSvL2 15.2.1", "category": "switch"},
        ]
    }
    # LITERAL_API_VALUES is forced "true" for the whole suite (see this
    # file's own top-of-conftest comment) so the real-fetch code path
    # needs its own override to actually run here.
    with (
        patch.object(topology_write.Settings.API, "LITERAL_API_VALUES", False),
        patch("app.topology_write.APIHandler.get", return_value=fake_response),
    ):
        result = topology_write._fetch_gns3_templates()

    assert result == fake_response["templates"]


def test_fetch_gns3_templates_tolerates_server_failure(app_state):
    from app import topology_write

    with (
        patch.object(topology_write.Settings.API, "LITERAL_API_VALUES", False),
        patch(
            "app.topology_write.APIHandler.get",
            side_effect=TimeoutError("template server slow"),
        ),
    ):
        result = topology_write._fetch_gns3_templates()

    assert result == []


def test_fetch_gns3_templates_short_circuits_in_literal_mode(app_state):
    from app import topology_write

    with patch("app.topology_write.APIHandler.get") as mock_get:
        result = topology_write._fetch_gns3_templates()

    assert result == []
    mock_get.assert_not_called()


def test_gns3_template_categories_returns_name_to_category_mapping(app_state):
    from app import topology_write

    templates = [
        {"name": "Cisco IOSv 15.9(3)M6", "category": "router"},
        {"name": "Cisco IOSvL2 15.2.1", "category": "switch"},
        {"name": "no category here"},
    ]

    assert topology_write._gns3_template_categories(templates) == {
        "Cisco IOSv 15.9(3)M6": "router",
        "Cisco IOSvL2 15.2.1": "switch",
    }


def test_gns3_template_port_specs_includes_templates_with_a_fixed_port_format(
    app_state,
):
    from app import topology_write

    templates = [
        {
            "name": "Cisco IOSv 15.6(1)T",
            "adapters": 4,
            "port_name_format": "Gi0/{0}",
            "port_segment_size": 0,
            "first_port_name": "",
        },
        {
            "name": "Cisco IOSvL2 15.2.1",
            "adapters": 16,
            "port_name_format": "Gi{1}/{0}",
            "port_segment_size": 4,
            "first_port_name": "",
        },
    ]

    assert topology_write._gns3_template_port_specs(templates) == {
        "Cisco IOSv 15.6(1)T": {
            "adapters": 4,
            "port_name_format": "Gi0/{0}",
            "port_segment_size": 0,
            "first_port_name": "",
        },
        "Cisco IOSvL2 15.2.1": {
            "adapters": 16,
            "port_name_format": "Gi{1}/{0}",
            "port_segment_size": 4,
            "first_port_name": "",
        },
    }


def test_gns3_template_port_specs_excludes_templates_with_no_fixed_format(app_state):
    from app import topology_write

    # VPCS/Ethernet switch/Cloud/... and Docker-based templates all look
    # like this on the real Template-API server - GNS3 names their ports
    # dynamically, there's no fixed format to compute from.
    templates = [
        {"name": "VPCS", "adapters": None, "port_name_format": None},
        {"name": "Ethernet switch", "adapters": None, "port_name_format": None},
        {"name": "alpine", "adapters": 1, "port_name_format": None},
    ]

    assert topology_write._gns3_template_port_specs(templates) == {}
