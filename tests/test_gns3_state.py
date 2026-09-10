from unittest.mock import patch

import pytest

from app.gns3_state import NoLiveProject, list_live_nodes, list_node_ports


def test_raises_when_no_project_resolved(app_state):
    app_state.project_id = None
    app_state.startup_warning = "no project deployed"

    class FakeApp:
        state = app_state

    with pytest.raises(NoLiveProject, match="no project deployed"):
        list_live_nodes(FakeApp())


def test_returns_live_nodes_when_project_resolved(app_state):
    app_state.project_id = "proj-1"

    class FakeApp:
        state = app_state

    fake_nodes = [{"name": "R1", "status": "started"}]
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes", return_value=fake_nodes
    ):
        assert list_live_nodes(FakeApp()) == fake_nodes


def test_list_node_ports_marks_in_use_from_live_links(app_state):
    app_state.project_id = "proj-1"

    class FakeApp:
        state = app_state

    fake_nodes = [
        {
            "name": "R1",
            "node_id": "n1",
            "ports": [
                {"name": "gi0/0", "adapter_number": 0, "port_number": 0},
                {"name": "gi0/1", "adapter_number": 0, "port_number": 1},
            ],
        }
    ]
    fake_links = [
        {"nodes": [{"node_id": "n1", "adapter_number": 0, "port_number": 0}, {"node_id": "n2", "adapter_number": 0, "port_number": 0}]}
    ]
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes", return_value=fake_nodes
    ), patch(
        "app.gns3_state.GNS3Connection.list_project_links", return_value=fake_links
    ):
        ports = list_node_ports(FakeApp(), "R1")

    assert ports == [
        {"name": "gi0/0", "in_use": True},
        {"name": "gi0/1", "in_use": False},
    ]


def test_list_node_ports_raises_for_unknown_node(app_state):
    app_state.project_id = "proj-1"

    class FakeApp:
        state = app_state

    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=[]):
        with pytest.raises(ValueError, match="no live GNS3 port information"):
            list_node_ports(FakeApp(), "GHOST")
