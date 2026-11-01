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
            {
                "node_id": "n1",
                "adapter_number": 0,
                "port_number": 0,
                "label": {"text": "eth0"},
            },
            {
                "node_id": "n2",
                "adapter_number": 0,
                "port_number": 0,
                "label": {"text": "Ethernet0"},
            },
        ],
    }
]


def _app_state(app_state):
    app_state.project_id = "proj-1"
    app_state.gns3_vm_ip = "10.20.20.233"
    return main_module.app


def test_list_live_links_reports_capturing_state(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
        ),
    ):
        links = capture.list_live_links(app)

    assert links == [
        {
            "node1": "R1",
            "if1": "eth0",
            "node2": "SW1",
            "if2": "Ethernet0",
            "port1": None,
            "port2": None,
            "capturing": False,
            "delay_ms": 0,
            "jitter_ms": 0,
            "loss_percent": 0,
            "corrupt_percent": 0,
            "suspended": False,
            "other_filters": [],
        }
    ]


def test_start_capture_posts_to_the_resolved_link(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
        ),
        patch("app.capture.GNS3Connection.ensure_project_open"),
        patch(
            "app.capture.GNS3Connection.post", return_value={"capturing": True}
        ) as fake_post,
    ):
        result = capture.start_capture(app, "R1", "SW1")

    assert result == {"capturing": True}
    fake_post.assert_called_once()
    (url,), kwargs = fake_post.call_args
    assert (
        url
        == "http://10.20.20.233:"
        + str(_gns3_port())
        + "/v2/projects/proj-1/links/link-1/start_capture"
    )
    assert kwargs["json"]["data_link_type"] == "DLT_EN10MB"


def test_start_capture_reopens_the_project_first(app_state):
    """
    Real bug found live: GNS3's own controller state can silently drift
    back to "closed" independently of node status, and every mutating
    per-link call (start_capture included) then 403s with "The project
    is not opened" - confirmed against the real GNS3 VM on testbox.
    """
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
        ),
        patch("app.capture.GNS3Connection.ensure_project_open") as fake_ensure_open,
        patch("app.capture.GNS3Connection.post", return_value={"capturing": True}),
    ):
        capture.start_capture(app, "R1", "SW1")

    fake_ensure_open.assert_called_once_with("10.20.20.233", _gns3_port(), "proj-1")


def test_stop_capture_posts_to_the_resolved_link(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
        ),
        patch("app.capture.GNS3Connection.ensure_project_open"),
        patch("app.capture.GNS3Connection.post") as fake_post,
    ):
        capture.stop_capture(app, "R1", "SW1")

    fake_post.assert_called_once()
    assert fake_post.call_args[0][0].endswith("/links/link-1/stop_capture")


def test_resolving_a_link_between_unknown_nodes_raises_value_error(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links", return_value=_LIVE_LINKS
        ),
    ):
        with pytest.raises(ValueError):
            capture.start_capture(app, "R1", "NoSuchNode")


def test_resolving_a_link_with_no_matching_gns3_link_raises_value_error(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch("app.capture.GNS3Connection.list_project_links", return_value=[]),
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


_PARALLEL_NODES = [
    {
        "name": "R1",
        "node_id": "n1",
        "ports": [
            {"name": "Gi0/0", "adapter_number": 0, "port_number": 0},
            {"name": "Gi0/1", "adapter_number": 1, "port_number": 0},
        ],
    },
    {
        "name": "SW1",
        "node_id": "n2",
        "ports": [
            {"name": "Gi0/0", "adapter_number": 0, "port_number": 0},
            {"name": "Gi0/1", "adapter_number": 1, "port_number": 0},
        ],
    },
]


def _parallel_links():
    def link(link_id, adapter):
        return {
            "link_id": link_id,
            "capturing": False,
            "nodes": [
                {
                    "node_id": "n1",
                    "adapter_number": adapter,
                    "port_number": 0,
                    "label": {"text": f"{adapter}/0"},
                },
                {
                    "node_id": "n2",
                    "adapter_number": adapter,
                    "port_number": 0,
                    "label": {"text": f"{adapter}/0"},
                },
            ],
        }

    return [link("link-a", 0), link("link-b", 1)]


def _resolve(app_state, *interfaces, links=None, nodes=None):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes",
            return_value=nodes or _PARALLEL_NODES,
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links",
            return_value=links or _parallel_links(),
        ),
    ):
        return capture._live_link(app, "R1", "SW1", *interfaces)


def test_parallel_links_are_told_apart_by_their_interfaces(app_state):
    assert _resolve(app_state, "Gi0/1", "Gi0/1")["link_id"] == "link-b"
    assert _resolve(app_state, "Gi0/0", "Gi0/0")["link_id"] == "link-a"
    assert (
        _resolve(app_state, "Gi0/1", None)["link_id"] == "link-b"
    )  # one side is enough
    assert (
        _resolve(app_state)["link_id"] == "link-a"
    )  # no interfaces: the first one, as before


def test_an_interface_that_matches_no_parallel_link_is_refused_instead_of_guessed(
    app_state,
):
    with pytest.raises(ValueError, match="no single live link"):
        _resolve(app_state, "Gi9/9", "Gi9/9")


def test_a_single_link_is_not_used_for_interfaces_that_do_not_belong_to_it(app_state):
    # the topology file may list a second link that GNS3 never got - the one live link is not it
    with pytest.raises(ValueError, match="no single live link"):
        _resolve(app_state, "Gi0/1", "Gi0/1", links=_parallel_links()[:1])
    assert (
        _resolve(app_state, "Gi0/0", "Gi0/0", links=_parallel_links()[:1])["link_id"]
        == "link-a"
    )


def test_a_cloud_node_port_name_does_not_narrow_the_choice_down(app_state):
    nodes = [
        dict(_PARALLEL_NODES[0]),
        {
            **_PARALLEL_NODES[1],
            "node_type": "cloud",
            "ports": [{"name": "eth0", "adapter_number": 0, "port_number": 0}],
        },
    ]
    assert (
        _resolve(
            app_state, "Gi0/0", "ens160", links=_parallel_links()[:1], nodes=nodes
        )["link_id"]
        == "link-a"
    )


def test_live_links_report_the_port_names_of_both_ends(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes",
            return_value=_PARALLEL_NODES,
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links",
            return_value=_parallel_links(),
        ),
    ):
        links = capture.list_live_links(app)

    assert [(link["port1"], link["port2"]) for link in links] == [
        ("Gi0/0", "Gi0/0"),
        ("Gi0/1", "Gi0/1"),
    ]


def test_the_capture_file_name_differs_per_parallel_link():
    assert capture._capture_file_name("R1", "SW1", None, None) == "R1_SW1.pcap"
    assert (
        capture._capture_file_name("R1", "SW1", "Gi0/0", "Gi0/1")
        == "R1_Gi0-0_SW1_Gi0-1.pcap"
    )


def test_start_capture_targets_only_the_chosen_parallel_link(app_state):
    app = _app_state(app_state)
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes",
            return_value=_PARALLEL_NODES,
        ),
        patch(
            "app.capture.GNS3Connection.list_project_links",
            return_value=_parallel_links(),
        ),
        patch("app.capture.GNS3Connection.ensure_project_open"),
        patch("app.capture.GNS3Connection.post") as fake_post,
    ):
        capture.start_capture(app, "R1", "SW1", "Gi0/1", "Gi0/1")

    assert fake_post.call_args[0][0].endswith("/links/link-b/start_capture")
    assert (
        fake_post.call_args[1]["json"]["capture_file_name"] == "R1_Gi0-1_SW1_Gi0-1.pcap"
    )
