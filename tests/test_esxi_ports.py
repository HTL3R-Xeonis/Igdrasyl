"""The interfaces of a live ESXi VM come from its own network adapters (app/esxi_ports.py), not from a typed name."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pyVmomi import vim

from app import esxi_ports


def _nic(port_group, mac="00:50:56:aa:bb:cc"):
    device = MagicMock(spec=vim.vm.device.VirtualVmxnet3)
    device.backing = SimpleNamespace(deviceName=port_group)
    device.macAddress = mac
    return device


def _vm(*devices):
    vm = MagicMock()
    vm.config.hardware.device = [*devices, MagicMock(spec=vim.vm.device.VirtualDisk)]
    return vm


def _app(vm, interfaces=None, node_name="PC4"):
    node = SimpleNamespace(interfaces=interfaces or {})
    app = MagicMock()
    app.state.esxi_connection.get_vm.return_value = vm
    app.state.graph = SimpleNamespace(nodes={node_name: node})
    return app


def _link(peer="SW1"):
    return SimpleNamespace(neighbour=SimpleNamespace(name=peer))


def test_the_adapters_on_the_nodes_port_groups_are_its_interfaces():
    vm = _vm(_nic("PC4_eth0"), _nic("PC4_eth1", "00:50:56:aa:bb:dd"))
    ports = esxi_ports.list_esxi_node_ports(_app(vm), "PC4")

    assert [p["name"] for p in ports] == ["eth0", "eth1"]
    assert ports[1]["mac"] == "00:50:56:aa:bb:dd"


def test_an_interface_the_topology_links_is_marked_in_use():
    vm = _vm(_nic("PC4_eth0"), _nic("PC4_eth1"))
    app = _app(vm, {"eth0": _link(), "eth1": SimpleNamespace(neighbour=None)})

    ports = esxi_ports.list_esxi_node_ports(app, "PC4")

    assert {p["name"]: p["in_use"] for p in ports} == {"eth0": True, "eth1": False}


def test_a_slash_written_as_a_dash_gets_the_topologys_spelling_back():
    vm = _vm(_nic("PC4_gi0-1"))
    ports = esxi_ports.list_esxi_node_ports(_app(vm, {"gi0/1": _link()}), "PC4")

    assert [p["name"] for p in ports] == ["gi0/1"]


def test_adapters_on_other_networks_are_not_interfaces():
    vm = _vm(_nic("VM Network"), _nic("PC5_eth0"), _nic("PC4_eth0"), _nic(None))
    ports = esxi_ports.list_esxi_node_ports(_app(vm), "PC4")

    assert [p["name"] for p in ports] == ["eth0"]


def test_a_vm_that_is_not_on_esxi_is_reported():
    app = _app(None)
    with pytest.raises(ValueError, match="not found on ESXi"):
        esxi_ports.list_esxi_node_ports(app, "PC4")


def test_the_prefix_must_match_the_whole_node_name():
    # "PC4" must not claim the adapters of a node called "PC4_B"'s "eth0" only because the text begins the same way
    assert esxi_ports.interface_from_port_group("PC4", "PC4_B_eth0", []) == "B_eth0"
    assert esxi_ports.interface_from_port_group("PC4", "PC40_eth0", []) is None


def test_the_ports_route_asks_esxi_for_an_esxi_node():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app" / "routes" / "live.py"
    ).read_text(encoding="utf-8")
    start = source.index('@app.get("/nodes/{name}/ports")')
    body = source[start : source.index("@app.get", start + 10)]
    assert "node.env == Environment.ON_ESXI" in body
    assert "esxi_ports.list_esxi_node_ports(app, name)" in body
    assert (
        "LIVE_READ_ERRORS" in body
    )  # an unreachable ESXi is a 503 with its reason, not a silent fallback
