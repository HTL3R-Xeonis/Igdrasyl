from unittest.mock import MagicMock, patch

import pytest
import requests

from app.gns3_state import NoLiveProject, list_live_nodes, list_node_ports, resolve_project_id


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


def test_list_live_nodes_treats_a_stale_project_id_404_as_no_live_project(app_state):
    """
    Real bug found live: a non-incremental deploy can delete and
    recreate the whole GNS3 project (not just its nodes) under a new
    id, even with the same name. A project_id cached before that
    (app.state.project_id isn't None, it's just wrong now) used to blow
    up /nodes entirely with a raw 500 from the resulting 404, instead of
    degrading the same way a never-resolved project does.
    """
    app_state.project_id = "stale-id"

    class FakeApp:
        state = app_state

    response = MagicMock(status_code=404)
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes",
        side_effect=requests.HTTPError(response=response),
    ):
        with pytest.raises(NoLiveProject, match="stale-id"):
            list_live_nodes(FakeApp())


def test_list_live_nodes_reraises_non_404_http_errors(app_state):
    app_state.project_id = "proj-1"

    class FakeApp:
        state = app_state

    response = MagicMock(status_code=503)
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes",
        side_effect=requests.HTTPError(response=response),
    ):
        with pytest.raises(requests.HTTPError):
            list_live_nodes(FakeApp())


def test_resolve_project_id_finds_project_by_name():
    projects = [{"name": "other", "project_id": "p0"}, {"name": "phaseb", "project_id": "p1"}]
    with patch("app.gns3_state.GNS3Connection.list_all_projects", return_value=projects):
        assert resolve_project_id("10.0.0.1", "phaseb") == "p1"


def test_resolve_project_id_returns_none_when_not_found():
    with patch("app.gns3_state.GNS3Connection.list_all_projects", return_value=[]):
        assert resolve_project_id("10.0.0.1", "phaseb") is None
