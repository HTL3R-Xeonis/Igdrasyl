from unittest.mock import patch

import pytest

import app.main as main_module
from app import capture
from app.gns3_state import NoLiveProject

_LIVE_NODES = [
    {"name": "R1", "node_id": "n1"},
    {"name": "SW1", "node_id": "n2"},
]
_LIVE_LINKS = [
    {
        "link_id": "link-1",
        "capturing": False,
        "nodes": [
            {"node_id": "n1", "adapter_number": 0, "port_number": 0, "label": {"text": "eth0"}},
            {"node_id": "n2", "adapter_number": 0, "port_number": 0, "label": {"text": "Ethernet0"}},
        ],
    }
]


def _app_state(app_state):
    app_state.project_id = "proj-1"
    app_state.gns3_vm_ip = "10.20.20.233"
    return main_module.app


def test_list_live_links_reports_capturing_state(app_state):
    app = _app_state(app_state)
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES), patch(
        "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
    ):
        links = capture.list_live_links(app)

    assert links == [
        {"node1": "R1", "if1": "eth0", "node2": "SW1", "if2": "Ethernet0", "capturing": False}
    ]


def test_start_capture_posts_to_the_resolved_link(app_state):
    app = _app_state(app_state)
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES), patch(
        "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
    ), patch("app.capture.GNS3Connection.ensure_project_open"), patch(
        "app.capture.GNS3Connection.post", return_value={"capturing": True}
    ) as fake_post:
        result = capture.start_capture(app, "R1", "SW1")

    assert result == {"capturing": True}
    fake_post.assert_called_once()
    (url,), kwargs = fake_post.call_args
    assert url == "http://10.20.20.233:" + str(_gns3_port()) + "/v2/projects/proj-1/links/link-1/start_capture"
    assert kwargs["json"]["data_link_type"] == "DLT_EN10MB"


def test_start_capture_reopens_the_project_first(app_state):
    """
    Real bug found live: GNS3's own controller state can silently drift
    back to "closed" independently of node status, and every mutating
    per-link call (start_capture included) then 403s with "The project
    is not opened" - confirmed against the real GNS3 VM on testbox.
    """
    app = _app_state(app_state)
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES), patch(
        "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
    ), patch("app.capture.GNS3Connection.ensure_project_open") as fake_ensure_open, patch(
        "app.capture.GNS3Connection.post", return_value={"capturing": True}
    ):
        capture.start_capture(app, "R1", "SW1")

    fake_ensure_open.assert_called_once_with("10.20.20.233", _gns3_port(), "proj-1")


def test_stop_capture_posts_to_the_resolved_link(app_state):
    app = _app_state(app_state)
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES), patch(
        "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
    ), patch("app.capture.GNS3Connection.ensure_project_open"), patch(
        "app.capture.GNS3Connection.post"
    ) as fake_post:
        capture.stop_capture(app, "R1", "SW1")

    fake_post.assert_called_once()
    assert fake_post.call_args[0][0].endswith("/links/link-1/stop_capture")


def test_resolving_a_link_between_unknown_nodes_raises_value_error(app_state):
    app = _app_state(app_state)
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES), patch(
        "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
    ):
        with pytest.raises(ValueError):
            capture.start_capture(app, "R1", "NoSuchNode")


def test_resolving_a_link_with_no_matching_gns3_link_raises_value_error(app_state):
    app = _app_state(app_state)
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES), patch(
        "app.capture.GNS3Connection.list_project_links", return_value=[]
    ):
        with pytest.raises(ValueError):
            capture.start_capture(app, "R1", "SW1")


def test_capture_requires_a_live_project(app_state):
    app_state.project_id = None
    with pytest.raises(NoLiveProject):
        capture.start_capture(main_module.app, "R1", "SW1")


def _gns3_port() -> int:
    from src.settings import Settings

    return Settings.GNS3.PORT
