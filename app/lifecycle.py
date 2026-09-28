"""
Live node/VM power-state actions (start/stop/reload) - a read-mostly
counterpart to app/topology_write.py's YAML-mutating one. These never
touch the topology file, only live state, so none of TopologyBackend's
facade/reload-graph complexity applies here - this follows
app/gns3_state.py's own pattern instead.
"""

from __future__ import annotations

from fastapi import FastAPI
from src.connections.gns3_connection import GNS3Connection
from src.graph.environment import Environment
from src.settings import Settings

from app.gns3_state import list_live_nodes


def _live_gns3_node_id(app: FastAPI, name: str) -> str:
    live_node = next((n for n in list_live_nodes(app) if n["name"] == name), None)
    if live_node is None:
        raise ValueError(f"node {name!r} has no live GNS3 instance")
    return live_node["node_id"]


def _esxi_vm(app: FastAPI, name: str):
    vm = app.state.esxi_connection.get_vm(name)
    if vm is None:
        raise ValueError(f"VM {name!r} not found on ESXi")
    return vm


def start_node(app: FastAPI, name: str) -> None:
    node = app.state.graph.nodes.get(name)
    if node is None:
        raise ValueError(f"no node named {name!r}")
    if node.env == Environment.ON_GNS3:
        node_id = _live_gns3_node_id(app, name)
        GNS3Connection.start_node(
            app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id, node_id
        )
    else:
        app.state.esxi_connection.power_on_vm(_esxi_vm(app, name))


def stop_node(app: FastAPI, name: str) -> None:
    node = app.state.graph.nodes.get(name)
    if node is None:
        raise ValueError(f"no node named {name!r}")
    if node.env == Environment.ON_GNS3:
        node_id = _live_gns3_node_id(app, name)
        GNS3Connection.stop_node(
            app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id, node_id
        )
    else:
        app.state.esxi_connection.power_off_vm(_esxi_vm(app, name))


def reload_node(app: FastAPI, name: str) -> None:
    node = app.state.graph.nodes.get(name)
    if node is None:
        raise ValueError(f"no node named {name!r}")
    if node.env == Environment.ON_GNS3:
        node_id = _live_gns3_node_id(app, name)
        GNS3Connection.reload_node(
            app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id, node_id
        )
    else:
        app.state.esxi_connection.reset_vm(_esxi_vm(app, name))
