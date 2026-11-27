"""
The interfaces a live ESXi VM really has, read from ESXi itself - no VMware Tools and no guest login needed.

The Builder gives every interface of an ESXi node its own network adapter on a port group called "<node>_<interface>" (a "/" in
the interface name is written as "-", see TopologyBuilder's VirtualLan). So the adapters in the VM's own configuration already
say which interfaces exist: the picker for Add Link can offer exactly those, plus the one new adapter the next link would add.
(The names the guest itself uses - ens192, Ethernet0 - are another thing: app/guest_nics.py maps them for config pushes.)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pyVmomi import vim


def interface_from_port_group(
    node_name: str, port_group: str | None, known_interfaces: list[str]
) -> str | None:
    """The interface a port group stands for, or None if it is not one of this node's ("VM Network", another node's)."""
    prefix = f"{node_name}_"
    if not port_group or not port_group.startswith(prefix):
        return None
    suffix = port_group[len(prefix) :]
    # a "/" was written as "-": give back the topology's own spelling when the topology knows the interface
    for known in known_interfaces:
        if known.replace("/", "-") == suffix:
            return known
    return suffix


def list_esxi_node_ports(app: FastAPI, name: str) -> list[dict[str, Any]]:
    """
    The interfaces of the live ESXi VM `name`: for each adapter on one of the node's port groups its interface name, its MAC
    address and whether the topology links it already.
    :raises ValueError: no such VM on ESXi. ESXi connection errors propagate (the caller reports them).
    """
    vm = app.state.esxi_connection.get_vm(name)
    if vm is None:
        raise ValueError(f"VM {name!r} not found on ESXi")
    node = app.state.graph.nodes.get(name) if app.state.graph is not None else None
    interfaces = dict(node.interfaces) if node is not None else {}

    ports: dict[str, dict[str, Any]] = {}
    for device in vm.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualEthernetCard):
            continue
        port_group = getattr(device.backing, "deviceName", None)
        interface = interface_from_port_group(name, port_group, list(interfaces))
        if interface is None:
            continue
        linked = interfaces.get(interface)
        ports[interface] = {
            "name": interface,
            "in_use": linked is not None and linked.neighbour is not None,
            "mac": device.macAddress,
        }
    return [ports[key] for key in sorted(ports)]
