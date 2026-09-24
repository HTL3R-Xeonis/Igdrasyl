from unittest.mock import MagicMock, patch

import pytest
import requests

from app.gns3_state import (
    NoLiveProject,
    list_live_nodes,
    list_node_ports,
    reload_project,
    resolve_project_id,
)


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


def test_reload_project_closes_then_opens_in_order():
    calls = []
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=[]), patch(
        "app.gns3_state.GNS3Connection.post", side_effect=lambda url: calls.append(url)
    ):
        reload_project("10.0.0.1", 80, "proj-1")

    assert calls == [
        "http://10.0.0.1:80/v2/projects/proj-1/close",
        "http://10.0.0.1:80/v2/projects/proj-1/open",
    ]


def test_reload_project_propagates_http_errors_from_close_or_open():
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=[]), patch(
        "app.gns3_state.GNS3Connection.post",
        side_effect=requests.HTTPError("boom"),
    ):
        with pytest.raises(requests.HTTPError):
            reload_project("10.0.0.1", 80, "proj-1")


def test_reload_project_restarts_only_nodes_that_were_started_before():
    """
    Real bug found live: GNS3 stops every node as part of closing a
    project and does NOT restart them on reopen - a plain close+open
    silently powered off the whole topology (including, per GNS3's own
    node list, the ESXi-hosted nodes' Cloud-node wrappers - though the
    real VMs underneath were untouched, only their GNS3-side status
    field). reload_project must remember what was actually running and
    restart exactly that afterward.
    """
    nodes_before = [
        {"node_id": "n1", "name": "R1", "status": "started"},
        {"node_id": "n2", "name": "SW1", "status": "stopped"},
        {"node_id": "n3", "name": "PC1", "status": "started"},
    ]
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes", return_value=nodes_before
    ), patch("app.gns3_state.GNS3Connection.post"), patch(
        "app.gns3_state.GNS3Connection.start_node"
    ) as fake_start:
        reload_project("10.0.0.1", 80, "proj-1")

    started_ids = {call.args[3] for call in fake_start.call_args_list}
    assert started_ids == {"n1", "n3"}


def test_reload_project_continues_restarting_other_nodes_if_one_fails():
    nodes_before = [
        {"node_id": "n1", "name": "R1", "status": "started"},
        {"node_id": "n2", "name": "SW1", "status": "started"},
    ]
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes", return_value=nodes_before
    ), patch("app.gns3_state.GNS3Connection.post"), patch(
        "app.gns3_state.GNS3Connection.start_node", side_effect=RuntimeError("boom")
    ) as fake_start:
        reload_project("10.0.0.1", 80, "proj-1")  # must not raise

    assert fake_start.call_count == 2
