"""
Tests app/design.py (the pure in-memory sketch model) directly, and the
/design/* HTTP endpoints in app/main.py through the test client. No real
GNS3/ESXi/TopologyBackend anywhere here - the whole point of design mode
is that it never touches any of that.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

import pytest
import yaml

from app import design
from src.graph.environment import Environment


@pytest.fixture(autouse=True)
def _templates(request):
    """
    Environment.get_environment's own template lookup is process-global
    and @lru_cache'd - stub it and clear the cache around every test in
    this file so no test either hits the real template server or leaks
    a cached result into another test file.
    """
    if "no_templates_fixture" in request.keywords:
        yield
        return
    Environment._get_templates.cache_clear()
    with patch(
        "src.graph.environment.APIHandler.get_gns3_template_names",
        return_value={"Cisco IOSv 15.6(1)T", "VPCS"},
    ), patch(
        "src.graph.environment.APIHandler.get_esxi_template_names",
        return_value={"debian"},
    ):
        yield
    Environment._get_templates.cache_clear()


def test_add_node_assigns_environment_from_template_lookup():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "Cisco IOSv 15.6(1)T")
    design.add_node(state, "VM1", "VM", "debian")

    assert state.nodes["R1"].environment == "ON_GNS3"
    assert state.nodes["VM1"].environment == "ON_ESXI"


def test_add_node_rejects_unknown_image():
    state = design.new_state()
    with pytest.raises(ValueError, match="unknown image"):
        design.add_node(state, "R1", "ROUTER", "not-a-real-template")


def test_add_node_rejects_duplicate_name():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "VPCS")
    with pytest.raises(ValueError, match="already exists"):
        design.add_node(state, "R1", "ROUTER", "VPCS")


def test_add_link_wires_interfaces_both_ways():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "VPCS")
    design.add_node(state, "SW1", "SWITCH", "VPCS")

    design.add_link(state, "R1", "eth0", "SW1", "Ethernet0")

    assert state.nodes["R1"].interfaces["eth0"] == "SW1"
    assert state.nodes["SW1"].interfaces["Ethernet0"] == "R1"
    assert state.links == [("R1", "eth0", "SW1", "Ethernet0")]


def test_add_link_rejects_interface_already_in_use():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "VPCS")
    design.add_node(state, "SW1", "SWITCH", "VPCS")
    design.add_node(state, "SW2", "SWITCH", "VPCS")
    design.add_link(state, "R1", "eth0", "SW1", "Ethernet0")

    with pytest.raises(ValueError, match="already in use"):
        design.add_link(state, "R1", "eth0", "SW2", "Ethernet0")


def test_remove_node_cascades_to_links_and_layout():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "VPCS")
    design.add_node(state, "SW1", "SWITCH", "VPCS")
    design.add_link(state, "R1", "eth0", "SW1", "Ethernet0")
    state.layout["R1"] = {"x": 1, "y": 2}

    design.remove_node(state, "R1")

    assert "R1" not in state.nodes
    assert state.links == []
    assert "eth0" not in state.nodes["SW1"].interfaces
    assert "R1" not in state.layout


def test_remove_link_frees_interfaces_on_both_sides():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "VPCS")
    design.add_node(state, "SW1", "SWITCH", "VPCS")
    design.add_link(state, "R1", "eth0", "SW1", "Ethernet0")

    design.remove_link(state, "SW1", "R1")

    assert state.links == []
    assert state.nodes["R1"].interfaces == {}
    assert state.nodes["SW1"].interfaces == {}


def test_to_topology_yaml_dict_groups_by_role_and_image():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "Cisco IOSv 15.6(1)T")
    design.add_node(state, "R2", "ROUTER", "Cisco IOSv 15.6(1)T")
    design.add_link(state, "R1", "eth0", "R2", "eth0")

    data = design.to_topology_yaml_dict(state)

    assert data["nodes"] == [
        {"names": ["R1", "R2"], "role": "ROUTER", "image": "Cisco IOSv 15.6(1)T"}
    ]
    assert data["edges"] == [["R1", "eth0", "R2", "eth0"]]


def test_auto_layout_gives_every_node_a_distinct_position():
    # Real bug this replaced a grid-based workaround for: a topology
    # created all at once (see app/generate_jobs.py) has nowhere for
    # the operator to click each node into place, so without an
    # explicit layout every node fell back to the same default
    # position and rendered as one fully overlapping stack.
    state = design.new_state()
    for name in ["R1", "SW1", "SW2", "PC1"]:
        design.add_node(state, name, "ROUTER", "Cisco IOSv 15.6(1)T")
    design.add_link(state, "R1", "eth0", "SW1", "eth0")
    design.add_link(state, "SW1", "eth1", "PC1", "eth0")

    design.auto_layout(state)

    assert set(state.layout) == {"R1", "SW1", "SW2", "PC1"}
    positions = [(p["x"], p["y"]) for p in state.layout.values()]
    assert len(set(positions)) == 4


def test_auto_layout_places_directly_linked_nodes_closer_together():
    # A loose sanity check on the force-directed algorithm actually
    # being wired up (not just "produces distinct points") - two
    # directly-linked nodes should end up closer than two with no path
    # between them at all.
    state = design.new_state()
    for name in ["R1", "SW1", "Isolated"]:
        design.add_node(state, name, "ROUTER", "Cisco IOSv 15.6(1)T")
    design.add_link(state, "R1", "eth0", "SW1", "eth0")

    design.auto_layout(state)

    def distance(a, b):
        pa, pb = state.layout[a], state.layout[b]
        return ((pa["x"] - pb["x"]) ** 2 + (pa["y"] - pb["y"]) ** 2) ** 0.5

    assert distance("R1", "SW1") < distance("R1", "Isolated")


def test_to_dict_and_from_dict_roundtrip():
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "Cisco IOSv 15.6(1)T")
    design.add_node(state, "SW1", "SWITCH", "VPCS")
    design.add_link(state, "R1", "eth0", "SW1", "Ethernet0")
    state.layout["R1"] = {"x": 1.5, "y": 2.5}
    state.drawings = [{"id": 1, "type": "text", "text": "hi"}]

    restored = design.from_dict(design.to_dict(state))

    assert restored.nodes.keys() == state.nodes.keys()
    for name, node in state.nodes.items():
        restored_node = restored.nodes[name]
        assert restored_node.role == node.role
        assert restored_node.image == node.image
        assert restored_node.environment == node.environment
        assert restored_node.interfaces == node.interfaces
    assert restored.links == state.links
    assert restored.layout == state.layout
    assert restored.drawings == state.drawings


@pytest.mark.no_templates_fixture
def test_from_dict_does_not_call_the_template_service():
    """
    The whole reason app/design_store.py exists: restoring a saved sketch
    (e.g. right after a restart) must survive a template-service outage,
    not just avoid re-validating for style reasons - from_dict must never
    call Environment.get_environment at all.
    """
    data = {
        "nodes": [
            {
                "name": "R1",
                "role": "ROUTER",
                "image": "Cisco IOSv 15.6(1)T",
                "environment": "ON_GNS3",
                "interfaces": {},
            }
        ],
        "links": [],
        "layout": {},
        "drawings": [],
    }
    with patch(
        "src.graph.environment.APIHandler.get_gns3_template_names",
        side_effect=TimeoutError("template service unreachable"),
    ):
        restored = design.from_dict(data)
    assert restored.nodes["R1"].environment == "ON_GNS3"


# --- HTTP layer ---


def test_design_add_node_and_get_topology(client, app_state):
    resp = client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    assert resp.status_code == 200

    resp = client.get("/design/topology")
    assert resp.status_code == 200
    [node] = resp.json()
    assert node["name"] == "R1"
    assert node["environment"] == "ON_GNS3"


def test_design_add_node_bad_image_is_400(client, app_state):
    resp = client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "nope"})
    assert resp.status_code == 400


def test_design_nodes_ports_404s_so_frontend_falls_back_to_free_text(client, app_state):
    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    resp = client.get("/design/nodes/R1/ports")
    assert resp.status_code == 404


def test_design_link_and_layout_roundtrip(client, app_state):
    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    client.post("/design/nodes", json={"name": "SW1", "role": "SWITCH", "image": "VPCS"})
    resp = client.post(
        "/design/links",
        json={"node1": "R1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet0"},
    )
    assert resp.status_code == 200

    resp = client.post("/design/layout", json={"name": "R1", "x": 12.0, "y": 34.0})
    assert resp.status_code == 200
    assert client.get("/design/layout").json()["R1"] == {"x": 12.0, "y": 34.0}

    resp = client.delete("/design/links/R1/SW1")
    assert resp.status_code == 200
    assert app_state.design.links == []


def test_design_remove_node_unknown_is_404(client, app_state):
    resp = client.delete("/design/nodes/nope")
    assert resp.status_code == 404


def test_design_drawings_roundtrip(client, app_state):
    drawings = [{"id": 1, "type": "text", "x1": 0, "y1": 0, "text": "hi"}]
    resp = client.post("/design/drawings", json={"drawings": drawings})
    assert resp.status_code == 200
    assert client.get("/design/drawings").json() == drawings


def test_design_reset_clears_everything(client, app_state):
    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    resp = client.post("/design/reset")
    assert resp.status_code == 200
    assert client.get("/design/topology").json() == []


def test_design_export_produces_host_agnostic_topoproj(client, app_state):
    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    client.post("/design/nodes", json={"name": "SW1", "role": "SWITCH", "image": "VPCS"})
    client.post(
        "/design/links",
        json={"node1": "R1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet0"},
    )
    client.post("/design/layout", json={"name": "R1", "x": 5.0, "y": 6.0})

    resp = client.get("/design/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(zf.namelist()) == {
        "manifest.json", "topology.yaml", "settings.yml", "configs.json",
        "drawings.json", "layout.json",
    }
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == 1
    assert manifest["auto_config_push"] is False

    settings = yaml.safe_load(zf.read("settings.yml"))
    assert settings == {}

    topology = yaml.safe_load(zf.read("topology.yaml"))
    assert topology["edges"] == [["R1", "eth0", "SW1", "Ethernet0"]]

    layout = json.loads(zf.read("layout.json"))["layout"]
    assert layout == {"R1": {"x": 5.0, "y": 6.0}}


def test_design_export_is_importable_against_any_active_host(client, app_state, temp_topology_file):
    """
    apply_import's ESXi-target check must accept a bundle with no target
    at all (see project_bundle.apply_import's has_target guard) - proves
    the interaction, not just build_design_export's own shape.
    """
    import app.main as main_module
    from app import project_bundle

    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    export_resp = client.get("/design/export")

    class _FakeBackend:
        def deploy_topology(self, topology_file, topology_yaml_text):
            from pathlib import Path

            Path(topology_file).write_text(topology_yaml_text)

    app_state.topology_backend = _FakeBackend()
    project_bundle.apply_import(main_module.app, export_resp.content)
    assert "R1" in app_state.graph.nodes
